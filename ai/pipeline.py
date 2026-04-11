from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select, func

from ai.news_classifier import NewsClassifier, NewsItem, NewsScore
from storage.models import AIOutputLog, MacroObservation, NewsHeadline


@dataclass
class AIPipelineResult:
    news_scores: dict[str, float]
    macro_regime: str
    macro_confidence: float
    macro_payload: dict[str, Any]
    news_details: dict[str, dict[str, Any]]
    anomalies: list[dict[str, Any]]


class AIPipeline:
    def __init__(self, config: dict[str, Any] | None = None, classifier: NewsClassifier | None = None):
        cfg = config or {}
        self.config = cfg
        self.classifier = classifier or NewsClassifier()
        self.news_lookback_hours = int(cfg.get("news_lookback_hours", 24))
        self.news_limit = int(cfg.get("news_limit", 200))
        self.max_news_items_per_cycle = max(1, int(cfg.get("max_news_items_per_cycle", 40)))
        self.regime_strategy_gates = cfg.get("regime_strategy_gates", {})
        self.anomaly_cfg = cfg.get("anomaly_detection", {})
        self.cache_ttl_seconds = max(0, int(cfg.get("cache_ttl_seconds", 300)))
        self._last_result: AIPipelineResult | None = None
        self._last_result_ts: datetime | None = None
        self._last_symbols: tuple[str, ...] | None = None
        self._startup_validated = False
        self._first_run = True  # bypass cache and stale-check on first call

    async def compute(self, session_factory, symbols: list[str]) -> AIPipelineResult:
        norm_symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        if not self._startup_validated:
            try:
                await self.classifier.validate_startup()
            except Exception:  # noqa: BLE001
                pass
            self._startup_validated = True
        now = datetime.now(timezone.utc)
        symbols_key = tuple(sorted(norm_symbols))
        if (
            not self._first_run  # never skip on first call — always boot fresh
            and self.cache_ttl_seconds > 0
            and self._last_result is not None
            and self._last_result_ts is not None
            and self._last_symbols == symbols_key
            and (now - self._last_result_ts).total_seconds() < self.cache_ttl_seconds
        ):
            logger.info("ai.pipeline | returning cached result | age_s={:.1f}", (now - self._last_result_ts).total_seconds())
            return self._last_result
        # Auto-fetch fresh news from NewsAPI if DB is stale (no recent headlines).
        # On first run after startup, always refresh regardless of staleness threshold.
        await self._refresh_news_if_stale(session_factory, force=self._first_run)
        self._first_run = False
        news = await self._load_recent_news(session_factory)
        macro_regime, macro_conf, macro_payload = await self._compute_macro_regime(session_factory)
        news_scores, details, anomalies = await self._score_news(symbols=norm_symbols, rows=news)
        result = AIPipelineResult(
            news_scores=news_scores,
            macro_regime=macro_regime,
            macro_confidence=macro_conf,
            macro_payload=macro_payload,
            news_details=details,
            anomalies=anomalies,
        )
        self._last_result = result
        self._last_result_ts = now
        self._last_symbols = symbols_key
        return result

    async def persist(self, session_factory, result: AIPipelineResult) -> None:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            for symbol, score in result.news_scores.items():
                d = result.news_details.get(symbol, {})
                session.add(
                    AIOutputLog(
                        timestamp=now,
                        symbol=symbol,
                        context_type="news",
                        score=Decimal(str(score)),
                        confidence=Decimal(str(d.get("confidence", 0.0))),
                        event_type=str(d.get("event_type", "other")),
                        decay_hours=int(d.get("decay_hours", 24)),
                        rationale=str(d.get("rationale", ""))[:4000],
                        payload=d,
                        source=str(d.get("provider", "local")),
                        latency_ms=int(d.get("latency_ms", 0)) or None,
                        cost_estimate_gbp=Decimal(str(d.get("cost_estimate_gbp", 0))) or None,
                    )
                )
            session.add(
                AIOutputLog(
                    timestamp=now,
                    symbol=None,
                    context_type="macro",
                    score=None,
                    confidence=Decimal(str(result.macro_confidence)),
                    event_type="macro",
                    regime_label=result.macro_regime,
                    rationale=f"Macro regime: {result.macro_regime}",
                    payload=result.macro_payload,
                    source="system",
                )
            )
            for a in result.anomalies:
                session.add(
                    AIOutputLog(
                        timestamp=now,
                        symbol=a.get("symbol"),
                        context_type="anomaly",
                        score=Decimal(str(a.get("score", 0.0))),
                        confidence=Decimal(str(a.get("confidence", 0.0))),
                        event_type="anomaly",
                        rationale=str(a.get("reason", "Narrative anomaly detected"))[:4000],
                        payload=a,
                        source="system",
                    )
                )
            await session.commit()

    def allowed_strategy_names(self, macro_regime: str) -> set[str] | None:
        rules = self.regime_strategy_gates
        if not isinstance(rules, dict):
            return None
        allowed = rules.get(macro_regime)
        if not isinstance(allowed, list):
            return None
        return {str(x).strip() for x in allowed if str(x).strip()}

    async def _refresh_news_if_stale(self, session_factory, *, force: bool = False) -> None:
        """
        Fetch fresh headlines from NewsAPI if the DB has no news in the last hour.
        Pass force=True on startup to always fetch regardless of last fetch time.
        This makes the AI pipeline self-sufficient — no separate run_pipeline.py needed.
        """
        news_api_key = os.getenv("NEWS_API_KEY", "").strip()
        if not news_api_key:
            return
        try:
            if not force:
                stale_threshold = timedelta(hours=1)
                cutoff = datetime.now(timezone.utc) - stale_threshold
                async with session_factory() as session:
                    latest_q = await session.execute(
                        select(func.max(NewsHeadline.fetched_at))
                    )
                    latest_fetch = latest_q.scalar_one()
                # Make timezone-aware for comparison
                if latest_fetch is not None:
                    lf = latest_fetch if latest_fetch.tzinfo else latest_fetch.replace(tzinfo=timezone.utc)
                    if lf >= cutoff:
                        return  # DB is fresh enough
            # DB is stale — fetch now
            from data.pipeline import ingest_news
            import yaml
            from pathlib import Path
            pipeline_cfg_path = Path("config/data_pipeline.yaml")
            pipeline_cfg: dict = {}
            if pipeline_cfg_path.is_file():
                with pipeline_cfg_path.open(encoding="utf-8") as f:
                    pipeline_cfg = yaml.safe_load(f) or {}
            news_cfg = pipeline_cfg.get("news", {"enabled": True, "query": "market stocks equities crypto forex", "page_size": 100})
            news_cfg["enabled"] = True
            reason = "startup flush" if force else "stale"
            logger.info("ai.pipeline | news {} | fetching from NewsAPI", reason)
            await ingest_news(session_factory, news_cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai.pipeline | news refresh failed (non-fatal) | {}", exc)

    async def _load_recent_news(self, session_factory) -> list[NewsHeadline]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.news_lookback_hours)
        async with session_factory() as session:
            q = await session.execute(
                select(NewsHeadline)
                .where(NewsHeadline.published_at >= cutoff)
                .order_by(NewsHeadline.published_at.desc())
                .limit(self.news_limit)
            )
            rows = list(q.scalars().all())
            if rows:
                return rows
            # Fallback: if published_at window is empty (common with delayed feeds),
            # use most recently fetched headlines so intelligence panels are not blank.
            fq = await session.execute(
                select(NewsHeadline)
                .order_by(NewsHeadline.fetched_at.desc())
                .limit(self.news_limit)
            )
            return list(fq.scalars().all())

    async def _score_news(
        self,
        *,
        symbols: list[str],
        rows: list[NewsHeadline],
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        if not rows:
            return ({s: 0.0 for s in symbols}, {}, [])

        items: list[NewsItem] = []
        for row in rows[: self.max_news_items_per_cycle]:
            items.append(
                NewsItem(
                    headline=row.title,
                    source=row.source_name,
                    published_at=row.published_at.isoformat(),
                    body=row.description,
                )
            )
        scored = await self.classifier.score_batch(items)
        usable: list[NewsScore] = [s for s in scored if s is not None]
        if not usable:
            return ({s: 0.0 for s in symbols}, {}, [])

        details: dict[str, dict[str, Any]] = {}
        scores: dict[str, float] = {}
        anomalies: list[dict[str, Any]] = []
        min_sample = int(self.anomaly_cfg.get("min_sample_count", 3))
        disagreement_threshold = float(self.anomaly_cfg.get("disagreement_ratio_threshold", 0.6))
        high_impact = float(self.anomaly_cfg.get("high_impact_score_abs", 0.75))
        low_conf = float(self.anomaly_cfg.get("low_confidence_threshold", 0.45))

        # Build scoring universe: monitored symbols PLUS every symbol Claude
        # mentioned in affected_symbols (the "discovered" set).  This is
        # the key insight — news movers should show what the market is
        # actually moving, not just the narrow monitored list.
        monitored_set = set(symbols)
        discovered_set: set[str] = set()
        for ns in usable:
            for sym in ns.affected_symbols:
                s = sym.strip().upper()
                if s and len(s) <= 12:  # basic sanity: valid ticker length
                    discovered_set.add(s)
        all_symbols = list(monitored_set | discovered_set)

        def _score_symbol(symbol: str) -> None:
            sym_scores = [s for s in usable if symbol in s.affected_symbols]
            if not sym_scores:
                if symbol in monitored_set:
                    scores[symbol] = 0.0  # keep monitored symbols even if no match
                return
            weighted = []
            for s in sym_scores:
                bias_mult = 1.0 if s.directional_bias == "bullish" else (-1.0 if s.directional_bias == "bearish" else 0.0)
                weighted.append(s.sentiment * s.confidence * bias_mult)
            v = sum(weighted) / max(1, len(weighted))
            v = max(-1.0, min(1.0, v))
            top = max(sym_scores, key=lambda x: x.confidence)
            scores[symbol] = v
            directional = [s.directional_bias for s in sym_scores]
            bull_n = sum(1 for b in directional if b == "bullish")
            bear_n = sum(1 for b in directional if b == "bearish")
            disagree_ratio = min(bull_n, bear_n) / max(1, bull_n + bear_n)
            details[symbol] = {
                "confidence": top.confidence,
                "event_type": top.event_type,
                "decay_hours": top.decay_hours,
                "rationale": top.rationale,
                "headline": top.headline,
                "sample_count": len(sym_scores),
                "disagreement_ratio": disagree_ratio,
                "provider": getattr(top, "provider", "unknown"),
                "latency_ms": getattr(top, "latency_ms", 0),
                "cost_estimate_gbp": getattr(top, "cost_estimate_gbp", 0.0),
            }
            if len(sym_scores) >= min_sample and disagree_ratio >= disagreement_threshold:
                anomalies.append(
                    {
                        "symbol": symbol,
                        "kind": "conflicting_narrative",
                        "reason": "Conflicting bullish/bearish narrative detected",
                        "sample_count": len(sym_scores),
                        "disagreement_ratio": disagree_ratio,
                        "score": v,
                        "confidence": top.confidence,
                    }
                )
            if abs(v) >= high_impact and top.confidence <= low_conf:
                anomalies.append(
                    {
                        "symbol": symbol,
                        "kind": "high_impact_low_confidence",
                        "reason": "Large predicted impact with low confidence",
                        "sample_count": len(sym_scores),
                        "score": v,
                        "confidence": top.confidence,
                    }
                )

        for sym in all_symbols:
            _score_symbol(sym)

        discovered_count = len(discovered_set - monitored_set)
        non_zero = sum(1 for v in scores.values() if v != 0.0)
        logger.info(
            "ai.pipeline | scored {} monitored + {} discovered symbols | {} non-zero",
            len(monitored_set), discovered_count, non_zero,
        )
        return scores, details, anomalies

    async def _compute_macro_regime(self, session_factory) -> tuple[str, float, dict[str, Any]]:
        series = self.config.get("macro_series", ["FEDFUNDS", "CPIAUCSL"])
        history: dict[str, list[Decimal]] = {}
        async with session_factory() as session:
            for sid in series:
                q = await session.execute(
                    select(MacroObservation)
                    .where(MacroObservation.series_id == sid)
                    .order_by(MacroObservation.obs_date.desc())
                    .limit(6)
                )
                rows = list(q.scalars().all())
                vals = [Decimal(str(r.value)) for r in rows if r.value is not None]
                if vals:
                    history[sid] = vals
        if not history:
            return "neutral", 0.0, {"reason": "no_macro_data"}

        rates = history.get("FEDFUNDS", [])
        inflation = history.get("CPIAUCSL", [])
        rate_trend = self._trend_label(rates)
        inflation_trend = self._trend_label(inflation)

        if rate_trend == "down" and inflation_trend in {"flat", "down"}:
            regime = "risk_on_disinflation"
        elif rate_trend == "up" and inflation_trend == "up":
            regime = "risk_off_stagflation"
        elif rate_trend == "up":
            regime = "tightening"
        elif rate_trend == "down":
            regime = "easing"
        else:
            regime = "neutral"

        conf = 0.65 if regime != "neutral" else 0.4
        payload = {
            "series_used": list(history.keys()),
            "rate_trend": rate_trend,
            "inflation_trend": inflation_trend,
        }
        logger.info("ai.pipeline | macro regime={} conf={:.2f}", regime, conf)
        return regime, conf, payload

    @staticmethod
    def _trend_label(vals: list[Decimal]) -> str:
        if len(vals) < 2:
            return "flat"
        latest = vals[0]
        older = vals[-1]
        delta = latest - older
        if delta > Decimal("0.05"):
            return "up"
        if delta < Decimal("-0.05"):
            return "down"
        return "flat"
