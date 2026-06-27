"""
Orchestrate M2 ingestion: yfinance -> features -> validation -> Postgres;
NewsAPI + Alpha Vantage + Finnhub + Marketaux + FRED when API keys are set.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from data.features import compute_feature_columns, row_features_to_json_dict
from data.fred_client import fetch_series_observations, fred_fetch_wallclock
from data.alphavantage_client import fetch_news_sentiment
from data.finnhub_client import fetch_general_news
from data.marketaux_client import fetch_all_news
from data.newsapi_client import fetch_everything
from data.persist import (
    insert_news_ignore_duplicates,
    upsert_feature_snapshots,
    upsert_macro_observations,
)
from data.validation import validate_ohlcv_frame
from data.yfinance_fetch import fetch_history
from data.ingest_telemetry import record_provider_ingest
from storage.models import FeatureSnapshot


def _clean_api_key(raw: str | None) -> str:
    """
    Treat blank/comment placeholders as unset to avoid hard failures when
    .env contains copied inline comments.
    """
    v = (raw or "").strip()
    if not v or v.startswith("#"):
        return ""
    return v


def _is_non_retryable_provider_error(exc: Exception) -> bool:
    """
    External feed quota/payment failures are deterministic for the current
    billing window. Retrying them burns more quota-adjacent calls and clutters
    telemetry without improving the outcome.
    """
    msg = str(exc).lower()
    non_retryable_markers = (
        "402 payment required",
        "payment required",
        "daily rate limit",
        "rate limit is 25 requests per day",
        "standard api rate limit",
        "quota",
        "request limit",
        "limit reached",
    )
    return any(marker in msg for marker in non_retryable_markers)


def _safe_provider_error(exc: Exception) -> str:
    text = str(exc)
    replacements = (
        (r"(api_token=)[^&\s'\"]+", r"\1***"),
        (r"(apikey=)[^&\s'\"]+", r"\1***"),
        (r"(api_key=)[^&\s'\"]+", r"\1***"),
        (r"(token=)[^&\s'\"]+", r"\1***"),
        (r"(detected your api key as )([A-Za-z0-9_\-]+)", r"\1***"),
    )
    safe = text
    for pattern, repl in replacements:
        safe = re.sub(pattern, repl, safe, flags=re.IGNORECASE)
    return safe


def load_pipeline_config(path: str | Path | None = None) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    p = Path(path) if path else root / "config" / "data_pipeline.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _interval_from_section(section: dict[str, Any]) -> timedelta | None:
    sec = section.get("expected_interval_seconds")
    if sec is None:
        return None
    return timedelta(seconds=int(sec))


def _stale_from_section(section: dict[str, Any], *, daily: bool) -> timedelta | None:
    if daily:
        d = section.get("stale_after_days")
        if d is None:
            return None
        return timedelta(days=int(d))
    h = section.get("stale_after_hours")
    if h is None:
        return None
    return timedelta(hours=int(h))


async def _to_thread_with_retry(
    fn,
    *args,
    op_name: str,
    attempts: int = 5,
    min_delay_sec: float = 2.0,
    max_delay_sec: float = 60.0,
    **kwargs,
):
    """
    Retry blocking external calls with exponential backoff + jitter.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_non_retryable_provider_error(exc):
                logger.warning(
                    "data | retry | {} | non_retryable=true | error={}",
                    op_name,
                    _safe_provider_error(exc),
                )
                break
            if attempt >= attempts:
                break
            delay = min(max_delay_sec, min_delay_sec * (2 ** (attempt - 1)))
            delay += random.uniform(0, min(1.0, delay * 0.1))
            logger.warning(
                "data | retry | {} | attempt={}/{} | next_sleep_sec={:.1f} | error={}",
                op_name,
                attempt,
                attempts,
                delay,
                _safe_provider_error(exc),
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def ingest_symbol_yfinance(
    session_factory: async_sessionmaker[AsyncSession],
    cfg: dict[str, Any],
    symbol: str,
    *,
    backfill: bool,
) -> dict[str, Any]:
    section = cfg["backfill"] if backfill else cfg["incremental"]
    interval = str(section["interval"])
    period = str(section["period"])
    expected = _interval_from_section(section)
    stale = _stale_from_section(section, daily=backfill)
    max_gap = float(cfg.get("validation", {}).get("max_gap_multiplier", 7.0))

    df = await _to_thread_with_retry(
        fetch_history,
        symbol,
        op_name=f"yfinance:{symbol}:{interval}",
        interval=interval,
        period=period,
    )
    if df.empty:
        logger.warning("data | yfinance | empty | {} | {}", symbol, interval)
        return {
            "symbol": symbol,
            "timeframe": interval,
            "upserted": 0,
            "bars_total": 0,
            "rows_with_full_features": 0,
        }

    required_ohlc = {
        str(column).lower(): column
        for column in df.columns
        if str(column).lower() in {"open", "high", "low", "close"}
    }
    if len(required_ohlc) != 4:
        raise ValueError(f"incomplete OHLC columns for {symbol}: {list(df.columns)}")
    numeric_ohlc = df[
        [required_ohlc[name] for name in ("open", "high", "low", "close")]
    ].apply(lambda column: column.astype(float))
    valid_ohlc = np.isfinite(numeric_ohlc.to_numpy(dtype=float)).all(axis=1)
    dropped = int((~valid_ohlc).sum())
    if dropped:
        logger.warning(
            "data | yfinance | dropped incomplete OHLC | {} | {} | rows={}",
            symbol,
            interval,
            dropped,
        )
        df = df.loc[valid_ohlc]
    if df.empty:
        logger.warning("data | yfinance | no complete OHLC rows | {} | {}", symbol, interval)
        return {
            "symbol": symbol,
            "timeframe": interval,
            "upserted": 0,
            "bars_total": 0,
            "rows_with_full_features": 0,
        }

    feat = compute_feature_columns(df, cfg)
    v = validate_ohlcv_frame(
        feat,
        expected_interval=expected,
        max_gap_multiplier=max_gap,
        stale_after=stale,
    )
    vdict = v.to_json_dict()
    if not v.ok:
        logger.warning(
            "data | validation | {} | {} | issues={}",
            symbol,
            interval,
            v.issues,
        )

    rows: list[dict[str, Any]] = []
    last_ts = feat.index[-1]
    for ts, row in feat.iterrows():
        is_last = ts == last_ts
        ts_utc = ts.to_pydatetime()
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        else:
            ts_utc = ts_utc.astimezone(timezone.utc)
        feats = row_features_to_json_dict(row)
        rows.append(
            {
                "symbol": symbol[:32],
                "timeframe": interval[:8],
                "bar_timestamp": ts_utc,
                "open": Decimal(str(row["open"])),
                "high": Decimal(str(row["high"])),
                "low": Decimal(str(row["low"])),
                "close": Decimal(str(row["close"])),
                "volume": Decimal(str(row["volume"])),
                "features": feats,
                "validation": vdict if is_last else None,
                "data_source": str(cfg.get("data_source", "yfinance"))[:20],
            }
        )

    async with session_factory() as session:
        n = await upsert_feature_snapshots(session, rows)
        await session.commit()
    logger.info("data | features | upserted | {} | {} | bars={}", symbol, interval, n)
    core = ["rsi_14", "atr_14", "mom_10", "vol_ratio"]
    bb_cols = [c for c in feat.columns if str(c).startswith(("BBL_", "BBM_", "BBU_", "BBB_", "BBP_"))]
    core += bb_cols
    present = [c for c in core if c in feat.columns]
    if present:
        full_rows = int(feat[present].notna().all(axis=1).sum())
    else:
        full_rows = 0
    total = int(len(feat))
    logger.info(
        "data | completeness | {} | {} | full_features={}/{} ({:.1f}%)",
        symbol,
        interval,
        full_rows,
        total,
        (100.0 * full_rows / total) if total else 0.0,
    )
    return {
        "symbol": symbol,
        "timeframe": interval,
        "upserted": n,
        "bars_total": total,
        "rows_with_full_features": full_rows,
    }


async def feature_history_counts(
    session_factory: async_sessionmaker[AsyncSession],
    symbols: list[str],
    *,
    timeframe: str,
) -> dict[str, int]:
    """Return persisted bar counts for the requested symbols only."""
    normalized = list(dict.fromkeys(str(s).strip() for s in symbols if str(s).strip()))
    if not normalized:
        return {}
    async with session_factory() as session:
        result = await session.execute(
            select(FeatureSnapshot.symbol, func.count(FeatureSnapshot.id))
            .where(
                FeatureSnapshot.timeframe == str(timeframe),
                FeatureSnapshot.symbol.in_(normalized),
            )
            .group_by(FeatureSnapshot.symbol)
        )
    return {str(symbol): int(count) for symbol, count in result.all()}


async def ingest_news(session_factory: async_sessionmaker[AsyncSession], cfg: dict[str, Any]) -> None:
    block = cfg.get("news") or {}
    if not block.get("enabled", False):
        return
    newsapi_key = _clean_api_key(os.getenv("NEWS_API_KEY"))
    alphavantage_key = _clean_api_key(os.getenv("ALPHAVANTAGE_API_KEY"))
    finnhub_key = _clean_api_key(os.getenv("FINNHUB_API_KEY"))
    marketaux_key = _clean_api_key(os.getenv("MARKETAUX_API_TOKEN"))
    if not newsapi_key and not alphavantage_key and not finnhub_key and not marketaux_key:
        logger.info(
            "data | news | skipped | NEWS_API_KEY, ALPHAVANTAGE_API_KEY, FINNHUB_API_KEY, and MARKETAUX_API_TOKEN unset"
        )
        return

    q = str(block.get("query", "market"))
    page_size = int(block.get("page_size", 100))
    language = str(block.get("language", "en"))
    av_limit = int(block.get("alphavantage_limit", 200))
    finnhub_limit = int(block.get("finnhub_limit", 100))
    finnhub_category = str(block.get("finnhub_category", "general"))
    marketaux_limit = int(block.get("marketaux_limit", 100))
    marketaux_must_have_entities = bool(block.get("marketaux_must_have_entities", True))

    async def _fetch_newsapi():
        if not newsapi_key:
            return []
        return await _to_thread_with_retry(
            fetch_everything,
            newsapi_key,
            op_name="newsapi:everything",
            q=q,
            language=language,
            page_size=page_size,
        )

    async def _fetch_alphavantage():
        if not alphavantage_key:
            return []
        return await _to_thread_with_retry(
            fetch_news_sentiment,
            alphavantage_key,
            op_name="alphavantage:news_sentiment",
            limit=av_limit,
        )

    async def _fetch_finnhub():
        if not finnhub_key:
            return []
        return await _to_thread_with_retry(
            fetch_general_news,
            finnhub_key,
            op_name="finnhub:general_news",
            category=finnhub_category,
            limit=finnhub_limit,
        )

    async def _fetch_marketaux():
        if not marketaux_key:
            return []
        return await _to_thread_with_retry(
            fetch_all_news,
            marketaux_key,
            op_name="marketaux:news_all",
            language=language,
            limit=marketaux_limit,
            must_have_entities=marketaux_must_have_entities,
        )

    results = await asyncio.gather(
        _fetch_newsapi(),
        _fetch_alphavantage(),
        _fetch_finnhub(),
        _fetch_marketaux(),
        return_exceptions=True,
    )
    # (pipeline_source, article) so rows persist ``ingest_provider`` for per-feed latest-headline age.
    tagged: list[tuple[str, Any]] = []
    source_counts: dict[str, int] = {}
    for source, result in (
        ("newsapi", results[0]),
        ("alphavantage", results[1]),
        ("finnhub", results[2]),
        ("marketaux", results[3]),
    ):
        if isinstance(result, Exception):
            safe_error = _safe_provider_error(result)
            logger.warning("data | news | source failed | source={} | {}", source, safe_error)
            try:
                await record_provider_ingest(
                    session_factory, source, ok=False, error=safe_error[:2000]
                )
            except Exception:  # noqa: BLE001
                pass
            continue
        source_counts[source] = len(result)
        for a in result:
            tagged.append((source, a))
        n = len(result)
        if n > 0:
            try:
                await record_provider_ingest(session_factory, source, ok=True, rows=n)
            except Exception:  # noqa: BLE001
                pass
    if not tagged:
        logger.warning("data | news | skipped | all enabled sources failed or empty")
        return

    now = datetime.now(timezone.utc)
    rows = []
    for source, a in tagged:
        rows.append(
            {
                "content_hash": a.content_hash,
                "url": a.url,
                "title": a.title,
                "description": a.description,
                "source_name": a.source_name,
                "ingest_provider": source,
                "published_at": a.published_at,
                "fetched_at": now,
            }
        )
    async with session_factory() as session:
        await insert_news_ignore_duplicates(session, rows)
        await session.commit()
    logger.info("data | news | batch | total={} | per_source={}", len(rows), source_counts)


async def ingest_fred(session_factory: async_sessionmaker[AsyncSession], cfg: dict[str, Any]) -> None:
    block = cfg.get("fred") or {}
    if not block.get("enabled", False):
        return
    key = _clean_api_key(os.getenv("FRED_API_KEY"))
    if not key:
        logger.info("data | fred | skipped | FRED_API_KEY unset")
        return
    years = int(block.get("observation_lookback_years", 3))
    start = date.today() - timedelta(days=365 * years + 10)
    series_ids = list(block.get("series") or [])
    fetched_at = fred_fetch_wallclock()
    all_rows: list[dict[str, Any]] = []
    for sid in series_ids:
        sid = str(sid).strip()[:32]
        if not sid:
            continue
        try:
            obs = await _to_thread_with_retry(
                fetch_series_observations,
                key,
                sid,
                op_name=f"fred:{sid}",
                observation_start=start,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("data | fred | skipped series | {} | {}", sid, exc)
            continue
        for o in obs:
            all_rows.append(
                {
                    "series_id": sid,
                    "obs_date": o.obs_date,
                    "value": o.value,
                    "fetched_at": fetched_at,
                }
            )
    if not all_rows:
        return
    async with session_factory() as session:
        await upsert_macro_observations(session, all_rows)
        await session.commit()
    try:
        await record_provider_ingest(session_factory, "fred", ok=True, rows=len(all_rows))
    except Exception:  # noqa: BLE001
        pass
    logger.info("data | fred | upserted | observations={}", len(all_rows))


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    cfg: dict[str, Any],
    *,
    backfill: bool,
    backfill_symbols: set[str] | None = None,
    include_news: bool = True,
    include_fred: bool = True,
) -> None:
    stats: list[dict[str, Any]] = []
    warmup = {str(s).strip().upper() for s in (backfill_symbols or set()) if str(s).strip()}
    for sym in cfg.get("symbols") or []:
        sym = str(sym).strip()
        if not sym:
            continue
        symbol_backfill = backfill or sym.upper() in warmup
        stat = await ingest_symbol_yfinance(
            session_factory,
            cfg,
            sym,
            backfill=symbol_backfill,
        )
        stats.append(stat)
    if (backfill or warmup) and stats:
        for s in stats:
            if not backfill and str(s["symbol"]).upper() not in warmup:
                continue
            total = int(s["bars_total"])
            full = int(s["rows_with_full_features"])
            logger.info(
                "data | backfill_summary | {} | {} | full_feature_rows={}/{} ({:.1f}%)",
                s["symbol"],
                s["timeframe"],
                full,
                total,
                (100.0 * full / total) if total else 0.0,
            )
    if include_news:
        await ingest_news(session_factory, cfg)
    if include_fred:
        await ingest_fred(session_factory, cfg)


async def run_loop(
    session_factory: async_sessionmaker[AsyncSession],
    cfg: dict[str, Any],
    *,
    backfill_first: bool,
) -> None:
    if backfill_first:
        await run_once(session_factory, cfg, backfill=True)
    interval = int(cfg.get("loop_interval_seconds", 3600))
    while True:
        await run_once(session_factory, cfg, backfill=False)
        logger.info("data | loop | sleep | {}s", interval)
        await asyncio.sleep(interval)
